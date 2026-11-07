"""Reuse finished shape candidates for luminance continuity and compact monochrome video."""

from collections import defaultdict
from datetime import datetime,timezone
from fractions import Fraction
from pathlib import Path
import argparse
import json
import shutil

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..render import render,LAYOUT_VERSION
from ..variants.pipeline import write_table
from .control import check_pause
from .full import assets
from .temporal import descriptors,path,LUMINANCE_VERSION


def produce(config,output,resume=False):
    source_root,root = Path(config["source"]),Path(output).resolve()
    source = read_manifest(source_root)
    if source["status"] not in ("complete","candidates_ready"):raise ValueError("Use completed shape candidates")
    if config["continuity_weight"]<0:raise ValueError("Continuity weight must be nonnegative")
    run = Path(source["config"]["run"])
    original = read_manifest(run)
    pause_file = root/"pause.request"
    run_id = identity([source["production_id"],config,LUMINANCE_VERSION,LAYOUT_VERSION])
    with writer_lock(root):
        state = read_manifest(root) if (root/"manifest.json").exists() else {
            "production_id":run_id,"config":config,"source_run":str(source_root),
            "source_production_id":source["production_id"],"descriptor":LUMINANCE_VERSION,
            "search_reused":True,"shape_scores_reused":True,"started_at":datetime.now(timezone.utc).isoformat()}
        if state["production_id"]!=run_id:raise ValueError("Use another output for different luminance settings")
        if state.get("status")=="complete":return state
        if resume:
            if pause_file.exists():pause_file.unlink()
            state.pop("error_type",None)
            state.pop("paused_at",None)
        runtime = {**config,"pause_file":str(pause_file)}
        def stage(name):
            state.update(status=name,updated_at=datetime.now(timezone.utc).isoformat())
            write_json(root/"manifest.json",state)
            check_pause(pause_file)
        try:
            stage("luminance-continuity")
            targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
                       if Fraction(r["source_pts"])*Fraction(r["source_time_base"])<Fraction(str(source["config"]["end_seconds"]))]
            pools = defaultdict(list)
            for row in pq.read_table(source_root/"choices.parquet").to_pylist():pools[row["slot"]].append(row)
            positions = {r["dense_row"] for rows in pools.values() for r in rows}
            positions.update(pq.read_table(source_root/"display.parquet",columns=["dense_row"])["dense_row"].to_pylist())
            table = pq.read_table(source_root/"candidates.parquet")
            table = table.filter(pc.is_in(table["dense_row"],value_set=pa.array(sorted(positions),type=pa.int64())))
            metadata = {r["dense_row"]:r for r in table.to_pylist()}
            if not (root/"selected.parquet").exists():
                needed = {r["dense_row"] for rows in pools.values() for r in rows}
                target_paths = {r["slot"]:str((run/"targets"/r["frame"]).resolve()) for r in targets}
                measured = descriptors(root,run,{p:metadata[p] for p in needed},pools,target_paths,
                                       config["workers"],str(pause_file),mode="luminance")
                candidates = [pools[f["slot"]] for f in targets]
                features = [np.stack([measured[f["slot"],r["dense_row"]] for r in rows]) for f,rows in zip(targets,candidates)]
                indices = path(candidates,features,config["continuity_weight"])
                chosen = [{**metadata[rows[i]["dense_row"]],**rows[i],"source_pts":f["source_pts"],
                           "source_time_base":f["source_time_base"],"selection_method":"luminance-continuous",
                           "selection_score":rows[i]["shape_score"]} for f,rows,i in zip(targets,candidates,indices)]
                previous = pq.read_table(source_root/"selected.parquet").to_pylist() if (source_root/"selected.parquet").exists() else None
                jump = {}
                for name,rows in (("previous_color_path",previous),("luminance_path",chosen)):
                    if rows is None:continue
                    sequence = [measured[r["slot"],r["dense_row"]] for r in rows]
                    jump[name] = float(np.mean([np.sum((a-b)**2) for a,b in zip(sequence,sequence[1:])]))
                state["selection_summary"] = {"changed_frames":sum(a["view_id"]!=b["view_id"] for a,b in zip(previous,chosen)) if previous else None,
                    "mean_luminance_jump":jump,"source_frames":len(chosen),"candidate_views":len(needed),
                    "mean_shape_score":float(np.mean([r["shape_score"] for r in chosen]))}
                write_table(root/"selected.parquet",chosen)
            for name in ("choices.parquet","display.parquet"):
                if not (root/name).exists():
                    try:(root/name).hardlink_to(source_root/name)
                    except OSError:shutil.copyfile(source_root/name,root/name)
            stage("assets")
            assets(root,run,targets,metadata,runtime,reuse_assets=Path(config.get("reuse_assets",source_root)))
            stage("render")
            for folder in (root/"targets",root/"search"):folder.mkdir(exist_ok=True)
            write_json(root/"targets/manifest.json",read_manifest(source_root/"targets"))
            write_json(root/"search/manifest.json",{"run_id":run_id,"candidate_count":read_manifest(Path(original["config"]["index"]))["rows"]})
            render_config = {**original["config"],**runtime,"end_seconds":source["config"]["end_seconds"],
                             "preset":"medium","crf":18,"audio_mode":"source_local"}
            result = render(root,render_config,pq.read_table(root/"frame-plan.parquet").to_pylist())
            state.update(status="complete",output=result,finished_at=datetime.now(timezone.utc).isoformat(),playback_review="not_performed")
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
    parser.add_argument("--config",default="config/silhouette-luminance.json")
    parser.add_argument("--output",default="data/runs/p6-silhouette-luminance-full")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--pause",action="store_true")
    args = parser.parse_args()
    if args.pause:
        root = Path(args.output)
        root.mkdir(exist_ok=True,parents=True)
        (root/"pause.request").touch()
    else:
        result = produce(json.loads(Path(args.config).read_text(encoding="utf-8")),args.output,args.resume)
        print(json.dumps({"status":result["status"],"summary":result.get("selection_summary")}),flush=True)
