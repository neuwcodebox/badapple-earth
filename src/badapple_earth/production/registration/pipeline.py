"""Resume global retrieval, valid camera alignment, temporal choice and full production."""

import argparse
from collections import defaultdict,Counter
from datetime import datetime,timezone
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import time

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest,members
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..assets import old_rgb_refs
from ..render import render
from ..silhouette.control import check_pause,batches
from ..silhouette.evaluation import resolve_candidates
from ..silhouette.full import assets
from ..variants.pipeline import write_table
from .search import retrieve,VERSION as SEARCH_VERSION
from .geometry import VERSION
from .selection import reduce_options,choose,display_options
from .workers import initialize,coarse_job,fine_job,prepare_targets
from .assets import reuse_native


def seeds(root,source,baseline,pixels,retrieval,config):
    file = root/"seeds.parquet"
    if file.exists():return pq.read_table(file).to_pylist()
    current = pq.read_table(baseline/"selected.parquet",columns=["slot","dense_row"]).to_pylist()
    pools = defaultdict(list)
    for row in pq.read_table(source/"choices.parquet",columns=["slot","dense_row","shape_score"]).to_pylist():pools[row["slot"]].append(row)
    offsets = np.rint(np.linspace(-config["neighbor_seconds"]*30000/1001,
                                  config["neighbor_seconds"]*30000/1001,config["neighbor_samples"])).astype(int)
    rows = []
    for frame in current:
        slot = frame["slot"]
        if np.std(pixels[slot].astype(np.float32)/255)<.005:continue
        addresses = set(map(int,retrieval[slot]))
        addresses.update(r["dense_row"] for r in sorted(pools[slot],key=lambda r:-r["shape_score"])[:8])
        addresses.update(current[int(np.clip(slot+offset,0,len(current)-1))]["dense_row"] for offset in offsets)
        addresses.add(frame["dense_row"])
        rows.extend({"slot":slot,"dense_row":p} for p in sorted(addresses))
    write_table(file,rows)
    return rows


def metadata(root,run,source,baseline,specs,seed_rows,workers,pause_file):
    destination = root/"base-candidates.parquet"
    if destination.exists():return {r["dense_row"]:r for r in pq.read_table(destination).to_pylist()}
    wanted = {r["dense_row"] for r in seed_rows}
    wanted.update(pq.read_table(baseline/"selected.parquet",columns=["dense_row"])["dense_row"].to_pylist())
    rows = None
    if (root/"candidates.parquet").exists():
        saved = pq.read_table(root/"candidates.parquet")
        if set(saved["dense_row"].to_pylist())==wanted:rows = {r["dense_row"]:r for r in saved.to_pylist()}
    if rows is None:rows = resolve_candidates(root,run,specs,sorted(wanted),workers,[str(source)],str(pause_file))
    original = read_manifest(run)["config"]
    index = Path(original["index"])
    directories = {m["index_id"]:p.resolve() for p,m,_ in members(index,read_manifest(index))}
    refs = old_rgb_refs(Path(original["extra_rgb_cache"]),{r["tile_id"] for r in rows.values() if not r.get("rgb_cache_ref")})
    resolved_files = {}
    for row in rows.values():
        directory = directories[row["index_member"]]
        if row["tile_id"] in refs:filename,offset = refs[row["tile_id"]]
        else:filename,offset = directory/row["rgb_cache_ref"],row["rgb_cache_row"]
        if filename not in resolved_files:resolved_files[filename] = str(filename.resolve())
        row.update(_rgb_filename=resolved_files[filename],_rgb_offset=offset,
                   _scene_filename=str(directory/"catalog"/(row["scene_id"]+".json")))
    write_table(destination,list(rows.values()))
    return rows


def coarse_groups(seed_rows,meta):
    grouped = {}
    for frame in seed_rows:
        row = meta[frame["dense_row"]]
        key = row["_rgb_filename"],row["_rgb_offset"],row["rotation_degrees"]
        if key not in grouped:grouped[key] = [row["dense_row"],set()]
        grouped[key][1].add(frame["slot"])
    files = defaultdict(list)
    for (filename,offset,angle),(address,slots) in sorted(grouped.items()):
        files[filename].append((offset,address,angle,sorted(slots)))
    return sorted(files.items())


def run_jobs(root,run,source,groups,mode,function,config):
    folder = root/mode
    folder.mkdir(exist_ok=True)
    chunks = [groups[i:i+8] for i in range(0,len(groups),8)]
    paths = [folder/f"{i:05d}.parquet" for i in range(len(chunks))]
    done = sum(p.exists() for p in paths)
    initial,started = done,time.perf_counter()
    def save(path,rows):
        nonlocal done
        write_table(path,rows)
        done += 1
        elapsed = time.perf_counter()-started
        write_json(root/"progress.json",{"stage":mode,"completed_batches":done,"total_batches":len(paths),
            "elapsed_seconds":round(elapsed,3),"remaining_seconds":round((len(paths)-done)*elapsed/max(done-initial,1))})
        print(f"Registration {mode}: {done}/{len(paths)} batches",flush=True)
    jobs = [((chunk,),paths[i]) for i,chunk in enumerate(chunks) if not paths[i].exists()]
    batches(jobs,function,save,config["workers"],str(root/"pause.request"),
            initializer=initialize,initargs=(str(run),str(source),config,mode))
    return paths


def fine_groups(paths,meta,config):
    pools = defaultdict(list)
    baselines = pq.read_table(Path(config["baseline"])/"selected.parquet",columns=["slot","tile_id"]).to_pylist()
    offsets = np.rint(np.linspace(-config["neighbor_seconds"]*30000/1001,
                                  config["neighbor_seconds"]*30000/1001,config["neighbor_samples"])).astype(int)
    nearby = [{baselines[int(np.clip(slot+d,0,len(baselines)-1))]["tile_id"] for d in offsets}
              for slot in range(len(baselines))]
    current_best,neighbor_best = {},{}
    def key(row):return -row["coarse_score"],row["seed_dense_row"]
    for path in paths:
        for row in pq.read_table(path).to_pylist():
            slot = row["slot"]
            tile = meta[row["seed_dense_row"]]["tile_id"]
            mandatory = (current_best if tile==baselines[slot]["tile_id"] else
                         neighbor_best if tile in nearby[slot] else None)
            if mandatory is not None and (slot not in mandatory or key(row)<key(mandatory[slot])):mandatory[slot] = row
            pool = pools[row["slot"]]
            pool.append(row)
            pool.sort(key=key)
            del pool[config["refine_candidates"]:]
    grouped = defaultdict(list)
    for slot,rows in sorted(pools.items()):
        retained = []
        for row in [current_best.get(slot),neighbor_best.get(slot),*rows]:
            if row is not None and not any(r["seed_dense_row"]==row["seed_dense_row"] for r in retained):retained.append(row)
        for row in retained[:config["refine_candidates"]]:grouped[row["seed_dense_row"]].append((slot,row["parameters"]))
    files = defaultdict(list)
    for address,frames in sorted(grouped.items()):
        row = meta[address]
        files[row["_rgb_filename"]].append((row["_rgb_offset"],row,frames))
    return sorted(files.items())


def evaluated_display(root,source):
    """Reuse measured runners-up and their original photo metadata."""
    root,source = Path(root),Path(source)
    measured = pq.read_table(root/"display.parquet").to_pylist()
    positions = sorted({r["dense_row"] for r in measured})
    metadata_file = root/"candidates.parquet"
    if not metadata_file.exists():metadata_file = source/"candidates.parquet"
    metadata = {r["dense_row"]:r for r in pq.read_table(metadata_file,filters=[("dense_row","in",positions)]).to_pylist()}
    return [{**metadata[r["dense_row"]],**r} for r in measured]


def select(root,run,baseline,meta,paths,pixels,config,production_id):
    if (root/"selected.parquet").exists():return
    baselines = pq.read_table(baseline/"selected.parquet").to_pylist()
    needed = {(r["slot"],r["dense_row"]) for r in baselines}
    luminance = {}
    for path in sorted((baseline/"luminance").glob("*.parquet")):
        for row in pq.read_table(path).to_pylist():
            key = row["slot"],row["dense_row"]
            if key in needed:luminance[key] = row["luminance"]
    for row in baselines:
        row["luminance"] = luminance[row["slot"],row["dense_row"]]
        row["base_dense_row"] = row["dense_row"]
    options = []
    for filename in paths:options.extend(pq.read_table(filename).to_pylist())
    pools = reduce_options(options,baselines,config)
    chosen = choose(pools,pixels,config)
    shown = display_options([*baselines,*options,*evaluated_display(baseline,config["source"])],chosen)
    write_selection(root,run,baselines,pools,chosen,read_manifest(root/"retrieval")["view_count"],shown=shown)


def write_selection(root,run,baselines,pools,chosen,view_count,method="registered-luminance-motion",shown=None):
    values,display = {},[]
    next_address = view_count
    by_view = {}
    shown = shown if shown is not None else display_options([r for rows in pools for r in rows],chosen)
    for rows in [*pools,*shown]:
        for row in rows:
            if row["view_id"] not in by_view:
                if row.get("registration_parameters") is not None:
                    by_view[row["view_id"]] = next_address
                    next_address += 1
                else:by_view[row["view_id"]] = row["dense_row"]
            row["dense_row"] = by_view[row["view_id"]]
            values[row["dense_row"]] = row
    display = [r for rows in shown for r in rows]
    targets = pq.read_table(run/"targets/targets.parquet",columns=["slot","source_pts","source_time_base"]).to_pylist()
    for row in chosen:
        target = targets[row["slot"]]
        row.update(source_pts=target["source_pts"],source_time_base=target["source_time_base"],
                   selection_method=method,selection_score=row["shape_score"])
    write_table(root/"candidates.parquet",list(values.values()))
    write_table(root/"choices.parquet",[r for rows in pools for r in rows])
    write_table(root/"display.parquet",display)
    write_table(root/"selected.parquet",chosen)
    summary = {"source_frames":len(chosen),"registered_frames":sum(r.get("registration_parameters") is not None for r in chosen),
        "changed_frames":sum(a["view_id"]!=b["view_id"] for a,b in zip(baselines,chosen)),
        "mean_shape_score":float(np.mean([r["shape_score"] for r in chosen])),
        "baseline_mean_shape_score":float(np.mean([r["shape_score"] for r in baselines])),
        "visible_frames":sum(r.get("visible",False) for r in chosen),
        "baseline_visible_frames":sum(r.get("visible",False) for r in baselines),
        "photo_changes":sum(a["tile_id"]!=b["tile_id"] for a,b in zip(chosen,chosen[1:])),
        "baseline_photo_changes":sum(a["tile_id"]!=b["tile_id"] for a,b in zip(baselines,baselines[1:])),
        "source_frame_counts":dict(Counter(r["source_id"] for r in chosen)),
        "selected_views":len({r["view_id"] for r in chosen}),"candidate_views":len(values)}
    write_json(root/"selection-summary.json",summary)


def verify(root,targets,baseline):
    selected = pq.read_table(root/"selected.parquet").to_pylist()
    if len(selected)!=len(targets):raise ValueError("Output frame count differs from source")
    for row,target in zip(selected,targets):
        if (row["slot"]!=target["slot"] or Fraction(row["source_pts"])*Fraction(row["source_time_base"])!=
                Fraction(target["source_pts"])*Fraction(target["source_time_base"])):raise ValueError("Original source PTS changed")
    probes = {}
    for filename,size in (("final-16x9.mp4",(1920,1080)),("satellite-full.mp4",(1444,1080))):
        result = subprocess.run(["ffprobe","-v","error","-show_streams","-show_format","-of","json",str(root/filename)],
                                capture_output=True,text=True,check=True)
        probe = json.loads(result.stdout)
        video = next(s for s in probe["streams"] if s["codec_type"]=="video")
        audio = next(s for s in probe["streams"] if s["codec_type"]=="audio")
        if (int(video["nb_frames"])!=len(targets) or (video["width"],video["height"])!=size
                or Fraction(video["avg_frame_rate"])!=Fraction(30000,1001) or audio["codec_name"]!="aac"):
            raise ValueError("Encoded video or original audio stream differs")
        probes[filename] = probe
    write_json(root/"verification.json",{"status":"complete","original_source_pts_preserved":True,
               "source_frames":len(selected),"output_probes":probes,"selection":read_manifest_file(root/"selection-summary.json"),
               "playback_review":"not_performed","verified_at":datetime.now(timezone.utc).isoformat()})


def read_manifest_file(path):return json.loads(path.read_text(encoding="utf-8"))


def produce(config,resume=False):
    root,source,baseline = map(Path,(config["output"],config["source"],config["baseline"]))
    original = read_manifest(source)
    if original["status"] not in ("complete","candidates_ready") or read_manifest(baseline)["status"]!="complete":
        raise ValueError("Registration requires finished baseline candidates and selection")
    run = Path(original["config"]["run"])
    production_id = identity([VERSION,SEARCH_VERSION,config,original["production_id"],read_manifest(baseline)["production_id"]])
    with writer_lock(root):
        state = read_manifest(root) if (root/"manifest.json").exists() else {
            "production_id":production_id,"config":config,"status":"starting","started_at":datetime.now(timezone.utc).isoformat()}
        if state["production_id"]!=production_id:raise ValueError("Registration output belongs to other settings")
        if state["status"]=="complete":return state
        if resume:
            (root/"pause.request").unlink(missing_ok=True)
            state.pop("error_type",None)
            state.pop("paused_at",None)
            state["resumed_at"] = datetime.now(timezone.utc).isoformat()
        def stage(name):
            state.update(status=name,updated_at=datetime.now(timezone.utc).isoformat())
            write_json(root/"manifest.json",state)
            check_pause(root/"pause.request")
        try:
            stage("retrieval")
            found = retrieve(config,root/"retrieval")
            targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
                       if Fraction(r["source_pts"])*Fraction(r["source_time_base"])<Fraction(str(config["end_seconds"]))]
            pixels = np.load(run/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)[:len(targets)]
            if not (root/"selected.parquet").exists():
                seed_rows = seeds(root,source,baseline,pixels,found["positions"],config)
                stage("metadata")
                specs = read_manifest_file(source/"retrieval/sources.json")
                meta = metadata(root,run,source,baseline,specs,seed_rows,config["workers"],root/"pause.request")
                stage("target-pixels")
                prepare_targets(run,root,len(targets))
                stage("coarse-alignment")
                paths = run_jobs(root,run,source,coarse_groups(seed_rows,meta),"coarse",coarse_job,config)
                stage("fine-alignment")
                paths = run_jobs(root,run,source,fine_groups(paths,meta,config),"fine",fine_job,config)
                stage("temporal-selection")
                select(root,run,baseline,meta,paths,pixels,config,production_id)
                del meta,seed_rows
            state["selection_summary"] = read_manifest_file(root/"selection-summary.json")
            table = pq.read_table(root/"candidates.parquet")
            meta = {r["dense_row"]:r for r in table.to_pylist()}
            runtime = {**config,"pause_file":str(root/"pause.request")}
            stage("assets")
            reuse_native(root,baseline,pq.read_table(root/"selected.parquet").to_pylist())
            assets(root,run,targets,meta,runtime,reuse_assets=baseline)
            stage("render")
            for folder in (root/"targets",root/"search"):folder.mkdir(exist_ok=True)
            write_json(root/"targets/manifest.json",read_manifest(baseline/"targets"))
            index = Path(read_manifest(run)["config"]["index"])
            write_json(root/"search/manifest.json",{"run_id":production_id,"candidate_count":read_manifest(index)["rows"]})
            render_config = {**read_manifest(run)["config"],**runtime,"preset":"medium","crf":18,"audio_mode":"source_local"}
            state["output"] = render(root,render_config,pq.read_table(root/"frame-plan.parquet").to_pylist())
            stage("verification")
            verify(root,targets,baseline)
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="config/registration-full.json")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--pause",action="store_true")
    args = parser.parse_args()
    config = read_manifest_file(Path(args.config))
    if args.pause:
        root = Path(config["output"]);root.mkdir(parents=True,exist_ok=True)
        (root/"pause.request").touch()
    else:print(json.dumps({"status":produce(config,args.resume)["status"]}),flush=True)
